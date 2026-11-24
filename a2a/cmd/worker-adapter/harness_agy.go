package main

import (
	"fmt"
	"slices"
	"strings"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib/persona/antigravity"
)

// agyBuilder is the Antigravity CLI's (`agy`) argv and env: the binary
// driven over its headless stream-json dialect (workeradapter's agyDriver).
//
// What it leaves to the other #6707 sub-issues, rather than inventing a
// mapping here:
//   - the delegate server and the system prompts: #6712. The tool grant is
//     here (#6711): agyPolicy reads the same Claude Code allowlist a session
//     pod's harnessCommand builds, and the builder renders it as agy's
//     --settings permissions. The model proxy is to enforce the offered
//     tools (#6714's follow-up PR), with these rules as defence in depth;
//     until it does, they are the only layer (lib/persona/antigravity);
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
	// agySettingsFlag carries the grant's permission rules, for this run
	// only (agy never saves them).
	agySettingsFlag = "--settings"
)

func (agyBuilder) defaultPath() string { return "" }

// command: `-p=` because agy's print switch takes a value, and the message
// comes on stdin as stream-json.
func (agyBuilder) command(spec harnessSpec) []string {
	argv := []string{
		spec.path,
		"-p=",
		"--input-format", "stream-json",
		"--output-format", "stream-json",
		agyPrintTimeoutFlag, agyPrintTimeout(spec.turnBudget).String(),
		agySettingsFlag, agyGrant(spec).Settings(),
	}
	return append(argv, spec.extra...)
}

// agyGrant is spec's tool grant as agy's. A grant that doesn't map or
// render is nothing granted. selectHarness refuses the inputs that could
// cause that (an A2A_ALLOWED_TOOLS override, A2A_WORKDIR) at startup, so
// this is the fail-closed floor, not a path a pod is expected to take.
//
// A profile worker's base argv (spec.tools false) carries this deny-all
// grant; the persona's grant (#6713) is to replace it, not add a second
// --settings.
func agyGrant(spec harnessSpec) antigravity.Grant {
	workdir := spec.workdir
	if workdir == "" {
		workdir = defaultWorkdir
	}
	var p antigravity.Policy
	if spec.tools {
		// agyPolicy returns nothing granted with its error.
		p, _ = agyPolicy(spec.allowed, spec.disallowed)
	}
	g, err := p.Render(workdir)
	if err != nil {
		g, _ = antigravity.Policy{}.Render(defaultWorkdir)
	}
	return g
}

// agyPolicy reads a Claude Code --allowedTools/--disallowedTools pair as a
// harness-neutral grant. A name the disallowed list carries grants nothing,
// as in Claude Code. TodoWrite has no agy equivalent and is dropped (agy's
// manage_task tracks background commands, not a to-do list); the delegate
// tool is dropped until #6712 gives agy its MCP server. Anything else this
// doesn't know, and an unrestricted Bash, is an error: no guess at a grant.
func agyPolicy(allowed, disallowed string) (antigravity.Policy, error) {
	deny := splitTools(disallowed)
	for _, d := range deny {
		// A scoped deny (Bash(kubectl delete:*)) would narrow a prefix grant
		// in Claude Code. No session pod sets one; refused rather than
		// ignored if one ever appears.
		if strings.Contains(d, "(") {
			return antigravity.Policy{}, fmt.Errorf("scoped disallowed tool %q has no agy form", d)
		}
	}
	var p antigravity.Policy
	for _, t := range splitTools(allowed) {
		base, arg, scoped := strings.Cut(t, "(")
		if slices.Contains(deny, base) {
			continue
		}
		switch {
		case !scoped && slices.Contains([]string{"Read", "Glob", "Grep", "LS"}, t):
			p.ReadFiles = true
		case t == "Write":
			p.WriteFiles = true
		case t == "WebSearch":
			p.WebSearch = true
		case t == "TodoWrite", t == delegateToolID:
		case base == "Bash" && scoped && strings.HasSuffix(arg, ":*)"):
			p.ShellPrefixes = append(p.ShellPrefixes, strings.TrimSuffix(arg, ":*)"))
		case t == "Bash":
			return antigravity.Policy{}, fmt.Errorf("an unrestricted Bash has no agy grant yet")
		default:
			return antigravity.Policy{}, fmt.Errorf("tool %q has no agy grant", t)
		}
	}
	return p, nil
}

// splitTools is a comma-separated tool list's entries, trimmed, empties
// dropped.
func splitTools(list string) []string {
	var out []string
	for _, t := range strings.Split(list, ",") {
		if t = strings.TrimSpace(t); t != "" {
			out = append(out, t)
		}
	}
	return out
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
