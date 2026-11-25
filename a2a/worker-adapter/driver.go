package workeradapter

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib/persona"
)

// Driver is everything the adapter knows about one harness's headless
// contract: how a user message goes onto its stdin, how its stdout lines map
// onto the adapter's events, how a session is resumed, how its MCP
// configuration names the delegate server, and which environment entry
// points it at a per-task config directory. Removing a reused pod's user
// settings file (Config.HarnessUserSettings) is still the adapter's. The supervision around it (the process group, the
// stdout ceiling, steering, cancel, the deadline, the terminal) is the
// adapter's and is the same for every harness.
//
// Drivers live in this package: the events they produce carry the adapter's
// own telemetry type. Claude Code is the first (claudeDriver); a nil
// Config.Driver means Claude Code.
type Driver interface {
	// Harness names the harness. It is also the AgentType on every message
	// the adapter publishes.
	Harness() persona.Harness
	// EncodeUser is one stdin line, without its newline, carrying text as a
	// user message: the opening prompt and every steer alike.
	EncodeUser(text string) ([]byte, error)
	// NewDecoder returns the stdout decoder for one harness process. A
	// decoder may keep state across lines (a harness that streams a turn in
	// pieces), so each process gets its own.
	NewDecoder() Decoder
	// WithResume is argv continuing harness session id, or argv unchanged
	// for "". It must not modify argv in place: argv is the pod's shared
	// Config value.
	WithResume(argv []string, id string) []string
	// WithDelegateServerEnv sets key to value in the delegate MCP server's
	// environment as argv configures it, and reports whether argv configured
	// that server at all. argv is not modified.
	WithDelegateServerEnv(argv []string, key, value string) ([]string, bool)
	// ConfiguresMCP reports whether argv hands the harness an MCP
	// configuration, which is when a missing delegate server entry is worth
	// a warning.
	ConfiguresMCP(argv []string) bool
	// ConfigDirEnv is the environment entry pointing the harness at a
	// per-task configuration directory.
	ConfigDirEnv(dir string) string
	// PrepareTurn runs before the harness starts and again at each turn
	// boundary that another turn follows: after a result line, before the
	// next user message is written. An error fails the task, and the next
	// turn does not run.
	PrepareTurn(ctx context.Context, dirs TurnDirs) error
	// HoldsSteers reports whether a steer that arrives while a turn is
	// running is held in the adapter until the turn's result and
	// PrepareTurn, rather than written onto stdin at once for the harness
	// to queue.
	HoldsSteers() bool
}

// TurnDirs are the directories a turn runs against, for Driver.PrepareTurn.
type TurnDirs struct {
	// Workspace is the harness's working directory.
	Workspace string
	// Home is HOME in the harness's environment, or "" when it sets none.
	Home string
}

// Decoder maps one harness stdout line onto zero or more events. An error
// is a line that is not the harness's output format at all: it is logged
// and skipped, never fatal, since the harness owns its stdout and the
// contract owns only the lines it can read.
type Decoder interface {
	Decode(line []byte) ([]Event, error)
}

// EventKind is what a harness event means to the adapter.
type EventKind int

const (
	// EventInit names the harness session (Event.Session) and its model
	// (Event.Model, Event.ModelDropped).
	EventInit EventKind = iota + 1
	// EventThinking is a piece of the model's thinking (Event.Text).
	EventThinking
	// EventProgress is a piece of the model's own prose (Event.Text).
	EventProgress
	// EventActivity is a tool invocation (Event.Tool, Event.Input).
	EventActivity
	// EventToolResult marks that one tool step's result is in, so the call
	// that ran it has exited. It carries no field. The adapter reads the
	// write-refusal record on it (refusal.go), never the result itself. A
	// driver emits exactly one per tool step.
	EventToolResult
	// EventResult ends a turn: Event.Text is the deliverable, Event.Failed
	// and Event.Token whether it failed and the reason token, and
	// Event.Telemetry what the turn cost.
	EventResult
)

// Event is one harness event in the adapter's own terms. Only the fields
// its Kind names are set.
type Event struct {
	Kind EventKind

	Session      string
	Model        string
	ModelDropped bool

	Text string

	Tool  string
	Input json.RawMessage
	// CallID is the harness's id for a tool invocation (EventActivity),
	// when it gives one: what a refused-call log record names.
	CallID string

	// Failed is a turn the harness reported as failed. Token is the reason
	// token a failed terminal carries (`reason: <token>`); an empty Token on
	// a failed result completes the task with its text, as it always has.
	Failed    bool
	Token     string
	Telemetry resultTelemetry
}

// driver is cfg's harness driver: Config.Driver, or Claude Code when unset.
func (cfg Config) driver() Driver {
	if cfg.Driver != nil {
		return cfg.Driver
	}
	return claudeDriver{}
}

// prepareFailedToken is the reason token of a task whose driver could not
// prepare its next turn (Driver.PrepareTurn).
const prepareFailedToken = "harness-prepare-failed"

// turnDirs is what a turn runs against, for Driver.PrepareTurn: the
// adapter's working directory, which the harness inherits, resolved to an
// absolute path with symlinks evaluated so a driver can compare paths under
// it; and HOME from env, the harness's environment (nil inherits the
// adapter's own). Either is "" when it cannot be found out.
func (a *adapter) turnDirs(env []string) TurnDirs {
	var dirs TurnDirs
	if wd, err := os.Getwd(); err == nil {
		if resolved, err := filepath.EvalSymlinks(wd); err == nil {
			dirs.Workspace = resolved
		}
	}
	if env == nil {
		dirs.Home = os.Getenv("HOME")
		return dirs
	}
	// The last entry wins, as it does for the process exec starts.
	for _, kv := range env {
		if v, ok := strings.CutPrefix(kv, "HOME="); ok {
			dirs.Home = v
		}
	}
	return dirs
}

// ErrNoHarness is DriverFor's answer for a selector that names no harness:
// the worker image carries none until another harness is available (#6707).
var ErrNoHarness = errors.New("no harness is available in this worker image")

// ErrHarnessRemoved is DriverFor's answer for claude-code, the prototype
// harness removed from the worker image (#6707). A pod or an older image tag
// that still names it is refused rather than started.
var ErrHarnessRemoved = errors.New("the claude-code harness was removed from the worker image")

// DriverFor is the driver for harness h, the value of the worker's harness
// selector. Antigravity is agyDriver and core-agent is coreAgentDriver; the
// worker refuses either without its isolation waiver (cmd/worker-adapter's
// selectHarness). Empty is
// ErrNoHarness and claude-code is ErrHarnessRemoved: neither starts a
// harness. Anything else is refused as unknown, never a fall back to another
// harness: a typo would otherwise run the wrong harness without a word.
func DriverFor(h persona.Harness) (Driver, error) {
	switch h {
	case "":
		// Absent is the image's harness, core-agent.
		return newCoreAgentDriver(), nil
	case persona.ClaudeCode:
		return nil, ErrHarnessRemoved
	case persona.Antigravity:
		return agyDriver{}, nil
	case persona.CoreAgent:
		return newCoreAgentDriver(), nil
	}
	return nil, fmt.Errorf("unknown harness %q (want %q or %q)", h, persona.Antigravity, persona.CoreAgent)
}

// turnRestarter is implemented by a driver whose harness takes one user
// turn per process: the adapter ends the process at each turn boundary and
// starts the next turn on a fresh one that resumes the conversation, so
// nothing the last turn started is still running when the driver prepares
// the next (Driver.PrepareTurn).
type turnRestarter interface {
	RestartsPerTurn() bool
}

func restartsPerTurn(d Driver) bool {
	r, ok := d.(turnRestarter)
	return ok && r.RestartsPerTurn()
}

// turnBudgeter is implemented by a driver whose argv carries a wall-clock
// bound for the harness, so a fresh process for a later turn gets the time
// left on the task rather than the whole of it.
type turnBudgeter interface {
	WithTurnBudget(argv []string, left time.Duration) []string
}
