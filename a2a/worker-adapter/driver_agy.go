package workeradapter

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strings"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/persona"
)

// agyDriver is the Antigravity CLI (`agy`) driver (#6709): its headless
// stream-json dialect, read off agy 1.5.13.
//
// What differs from Claude Code, and what it means here:
//   - stdin is {"event":"user","message":{"role":"user","content":[{"type":"text","text":...}]}};
//   - stdout is `init` (the conversation id), then `step_update`s, then one
//     `result` per user message;
//   - a session resumes with --conversation <id>;
//   - failure is in the result, never the exit code: agy exits 0 on an
//     ERROR result and on a --print-timeout expiry alike, and the expiry
//     even reports status SUCCESS, with finished_reason print_timeout;
//   - a tool call it could not get approval for in headless mode is
//     skipped and listed in the result's denied_actions while the result
//     still says SUCCESS, so a non-empty list fails the turn here;
//   - it streams no thinking text (only a thinking token count), so it
//     produces no EventThinking;
//   - it reports no cost, so its telemetry carries no running total and the
//     adapter publishes none (cost is absent for agy, by design here);
//   - result.usage is cumulative over the process, so the decoder reports
//     each result's difference from the last.
//
// Its MCP configuration (#6712), permissions (#6711), workspace and HOME
// isolation (#6710) and model routing (#6714) are other sub-issues of
// #6707; until they land the driver configures no MCP server and prepares
// nothing before a turn.
type agyDriver struct{}

var _ Driver = agyDriver{}

// agyResumeFlag continues an agy conversation by id.
const agyResumeFlag = "--conversation"

// The reason tokens of an agy result the turn failed on. A status other
// than SUCCESS reuses the generic token whose meaning matches; the other
// two are agy's own, and the gateway words them (relay.go reasonSentences).
const (
	agyErrorToken            = "error_during_execution"
	agyPrintTimeoutToken     = "print-timeout"
	agyPermissionDeniedToken = "permission-denied"
)

// agyPrintTimeoutReason is the finished_reason of a turn --print-timeout
// cut short: its status is SUCCESS and its response partial or empty.
const agyPrintTimeoutReason = "print_timeout"

func (agyDriver) Harness() persona.Harness { return persona.Antigravity }

func (agyDriver) EncodeUser(text string) ([]byte, error) {
	type part struct {
		Type string `json:"type"`
		Text string `json:"text"`
	}
	type message struct {
		Role    string `json:"role"`
		Content []part `json:"content"`
	}
	return json.Marshal(struct {
		Event   string  `json:"event"`
		Message message `json:"message"`
	}{Event: "user", Message: message{Role: "user", Content: []part{{Type: "text", Text: text}}}})
}

func (agyDriver) NewDecoder() Decoder { return &agyDecoder{} }

func (agyDriver) WithResume(argv []string, id string) []string {
	if id == "" {
		return argv
	}
	return append(slices.Clone(argv), agyResumeFlag, id)
}

// WithDelegateServerEnv reports the delegate server unconfigured: agy takes
// MCP servers through --settings, which #6712 renders.
func (agyDriver) WithDelegateServerEnv(argv []string, _, _ string) ([]string, bool) {
	return argv, false
}

func (agyDriver) ConfiguresMCP([]string) bool { return false }

// ConfigDirEnv points agy at a per-task HOME: its state, conversation store
// included, lives under $HOME/.gemini.
func (agyDriver) ConfigDirEnv(dir string) string { return "HOME=" + dir }

func (agyDriver) PrepareTurn(context.Context, TurnDirs) error { return nil }

// HoldsSteers is true: agy answers each stdin message as its own turn, and
// holding a steer to the boundary is where PrepareTurn can act before the
// next turn starts (#6710's per-turn workspace scrub).
func (agyDriver) HoldsSteers() bool { return true }

// agyLine is the part of an agy stdout line the decoder reads.
type agyLine struct {
	Event          string         `json:"event"`
	ConversationID string         `json:"conversation_id"`
	Init           *agyInit       `json:"init"`
	StepUpdate     *agyStepUpdate `json:"step_update"`
	Result         *agyResult     `json:"result"`
}

type agyInit struct {
	Model string `json:"model"`
}

type agyStepUpdate struct {
	StepType  string       `json:"step_type"`
	State     string       `json:"state"`
	TextDelta string       `json:"text_delta"`
	ToolName  string       `json:"tool_name"`
	ToolInfo  *agyToolInfo `json:"tool_info"`
}

type agyToolInfo struct {
	Parameters json.RawMessage `json:"parameters"`
}

type agyResult struct {
	Status          string            `json:"status"`
	Response        string            `json:"response"`
	Error           string            `json:"error"`
	FinishedReason  string            `json:"finished_reason"`
	DeniedActions   []agyDeniedAction `json:"denied_actions"`
	DurationSeconds json.RawMessage   `json:"duration_seconds"`
	Usage           json.RawMessage   `json:"usage"`
}

type agyDeniedAction struct {
	Action      string `json:"action"`
	DisplayName string `json:"display_name"`
}

// agyUsage is agy's token count. input_tokens excludes the cached prefix
// (cache_read_tokens); output_tokens already includes thinking_tokens.
type agyUsage struct {
	InputTokens     float64 `json:"input_tokens"`
	OutputTokens    float64 `json:"output_tokens"`
	CacheReadTokens float64 `json:"cache_read_tokens"`
}

// agyDecoder decodes one agy process's stdout. It keeps the last result's
// cumulative usage and duration, so each result reports its own turn.
type agyDecoder struct {
	model    string
	input    float64
	output   float64
	duration float64
}

var errNotAgyLine = errors.New("not an agy stream-json line")

func (d *agyDecoder) Decode(line []byte) ([]Event, error) {
	var ln agyLine
	if err := json.Unmarshal(line, &ln); err != nil || ln.Event == "" {
		return nil, errNotAgyLine
	}
	switch ln.Event {
	case "init":
		ev := Event{Kind: EventInit, Session: ln.ConversationID}
		if ln.Init != nil {
			d.model = ln.Init.Model
			ev.Model = ln.Init.Model
		}
		return []Event{ev}, nil
	case "step_update":
		return d.step(ln.StepUpdate), nil
	case "result":
		if ln.Result == nil {
			return nil, nil
		}
		return []Event{d.result(*ln.Result)}, nil
	}
	return nil, nil
}

// step maps a step_update onto progress and activity. The model's prose
// streams as agent_response text deltas; a tool step is announced once, when
// it starts. User input, system messages, error messages and a tool step's
// later states carry nothing the artifacts show; the result decides failure.
func (d *agyDecoder) step(s *agyStepUpdate) []Event {
	if s == nil {
		return nil
	}
	switch {
	case s.StepType == "agent_response" && s.TextDelta != "":
		return []Event{{Kind: EventProgress, Text: s.TextDelta}}
	case s.StepType == "tool" && s.State == "ACTIVE":
		var input json.RawMessage
		if s.ToolInfo != nil {
			input = s.ToolInfo.Parameters
		}
		return []Event{{Kind: EventActivity, Tool: s.ToolName, Input: input}}
	}
	return nil
}

func (d *agyDecoder) result(r agyResult) Event {
	ev := Event{Kind: EventResult, Text: r.Response, Telemetry: d.telemetry(r)}
	// A turn that also skipped tool calls says so in its detail, whichever
	// failure names it.
	var denied string
	if len(r.DeniedActions) > 0 {
		denied = deniedDetail(r.DeniedActions)
	}
	switch {
	case r.FinishedReason == agyPrintTimeoutReason:
		// Before the status: an expiry reports SUCCESS with a partial response.
		ev.Failed, ev.Token = true, withDetail(agyPrintTimeoutToken, r.Error, denied)
	case r.Status == "ERROR":
		ev.Failed, ev.Token = true, withDetail(agyErrorToken, r.Error, denied)
	case r.Status != "SUCCESS":
		// A status this driver does not know is still a failed turn, under
		// the generic token: the status itself goes in the detail, never into
		// a token built from text agy chose.
		ev.Failed, ev.Token = true, withDetail(agyErrorToken, "status "+r.Status, r.Error, denied)
	case denied != "":
		ev.Failed, ev.Token = true, withDetail(agyPermissionDeniedToken, denied)
	}
	return ev
}

// telemetry is this result's turn: the difference between its cumulative
// usage and duration and the last result's. agy reports no cost, so there
// is no running total. duration_seconds is wall time since the process
// started (the probe's second turn read 47.3s after a first of 18.0s and a
// 25s pause before its message), so the difference includes any wait between
// one result and the next message; with steers written at the boundary that
// wait is the adapter's own, and short.
func (d *agyDecoder) telemetry(r agyResult) resultTelemetry {
	t := resultTelemetry{usage: lib.Usage{Model: d.model}}
	var secs float64
	switch {
	case len(r.DurationSeconds) == 0:
	case json.Unmarshal(r.DurationSeconds, &secs) != nil:
		t.dropped = append(t.dropped, "duration_seconds")
	default:
		if ms, ok := lib.UsageFigure((secs - d.duration) * 1000); ok {
			t.usage.DurationMs = ms
		} else {
			t.dropped = append(t.dropped, "duration_seconds")
		}
		d.duration = secs
	}
	var u agyUsage
	switch {
	case len(r.Usage) == 0:
	case json.Unmarshal(r.Usage, &u) != nil:
		t.dropped = append(t.dropped, "usage")
	default:
		input := u.InputTokens + u.CacheReadTokens
		if n, ok := lib.UsageFigure(input - d.input); ok {
			t.usage.InputTokens = n
		} else {
			t.dropped = append(t.dropped, "usage.input_tokens")
		}
		if n, ok := lib.UsageFigure(u.OutputTokens - d.output); ok {
			t.usage.OutputTokens = n
		} else {
			t.dropped = append(t.dropped, "usage.output_tokens")
		}
		d.input, d.output = input, u.OutputTokens
	}
	return t
}

// agyDetailCap bounds the detail an agy failure carries into its reason:
// agy's error text is the upstream API's, and an HTML error page or a long
// list of skipped calls has no business in a one-line reason.
const agyDetailCap = 200

// withDetail is token, and the non-empty details joined after the reason
// separator, as the adapter's own terminals spell it: whitespace collapsed to
// single spaces so the reason stays one line, and the whole detail capped at
// agyDetailCap runes.
func withDetail(token string, details ...string) string {
	var parts []string
	for _, d := range details {
		if d = strings.Join(strings.Fields(d), " "); d != "" {
			parts = append(parts, d)
		}
	}
	if len(parts) == 0 {
		return token
	}
	detail := strings.Join(parts, "; ")
	if r := []rune(detail); len(r) > agyDetailCap {
		detail = string(r[:agyDetailCap-1]) + "…"
	}
	return token + reasonDetailSeparator + detail
}

// deniedDetail names the actions agy skipped: "command (Bash), mcp (a2a)".
func deniedDetail(actions []agyDeniedAction) string {
	names := make([]string, 0, len(actions))
	for _, a := range actions {
		switch {
		case a.Action != "" && a.DisplayName != "":
			names = append(names, fmt.Sprintf("%s (%s)", a.Action, a.DisplayName))
		case a.Action != "":
			names = append(names, a.Action)
		case a.DisplayName != "":
			names = append(names, a.DisplayName)
		}
	}
	return "skipped for want of approval: " + strings.Join(names, ", ")
}
