package workeradapter

import (
	"context"
	"encoding/json"
	"slices"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/persona"
)

// claudeDriver is Claude Code's headless stream-json contract: the native
// binary driven with --print and stream-json in and out (the argv
// cmd/worker-adapter builds).
type claudeDriver struct{}

const (
	// harnessResumeFlag continues an earlier harness session by id. The
	// pinned binary takes it with --print (`claude -p --resume <id>`), and
	// the agent SDK passes it alongside --input-format stream-json, the
	// shape harnessCommand builds.
	harnessResumeFlag = "--resume"
)

func (claudeDriver) Harness() persona.Harness { return persona.ClaudeCode }

func (claudeDriver) EncodeUser(text string) ([]byte, error) {
	return json.Marshal(userMessage{
		Type:    "user",
		Message: userMessageBody{Role: "user", Content: text},
	})
}

func (claudeDriver) NewDecoder() Decoder { return claudeDecoder{} }

func (claudeDriver) WithResume(argv []string, id string) []string { return withResume(argv, id) }

func (claudeDriver) WithDelegateServerEnv(argv []string, key, value string) ([]string, bool) {
	return withDelegateServerEnv(argv, key, value)
}

func (claudeDriver) ConfiguresMCP(argv []string) bool { return slices.Contains(argv, MCPConfigFlag) }

func (claudeDriver) ConfigDirEnv(dir string) string { return claudeConfigDirEnv + "=" + dir }

// PrepareTurn is a no-op: --setting-sources user and --strict-mcp-config keep
// the workspace's own settings out, and a reused pod removes the user
// settings file before each task (Config.HarnessUserSettings).
func (claudeDriver) PrepareTurn(context.Context, TurnDirs) error { return nil }

// HoldsSteers is false: the harness queues a steer written mid-turn to its
// next turn boundary itself.
func (claudeDriver) HoldsSteers() bool { return false }

// claudeConfigDirEnv points the harness at a per-task configuration
// directory: its user settings, and the rendered persona of a profile worker.
const claudeConfigDirEnv = "CLAUDE_CONFIG_DIR"

// claudeDecoder reads one stream-json line at a time; nothing carries over
// between lines.
type claudeDecoder struct{}

func (claudeDecoder) Decode(line []byte) ([]Event, error) {
	var ev harnessEvent
	if err := json.Unmarshal(line, &ev); err != nil {
		return nil, err
	}
	switch ev.Type {
	case "system":
		if ev.Subtype != "init" {
			return nil, nil
		}
		model, dropped := ev.model()
		return []Event{{Kind: EventInit, Session: ev.SessionID, Model: model, ModelDropped: dropped}}, nil
	case "assistant":
		return ev.assistantEvents(), nil
	case "user":
		return ev.toolResultEvents(), nil
	case "result":
		// The model is the init line's, which the adapter keeps; the
		// telemetry here carries the turn's own figures.
		return []Event{{
			Kind:      EventResult,
			Text:      ev.Result,
			Failed:    ev.IsError || ev.Subtype != "success",
			Token:     ev.Subtype,
			Telemetry: ev.telemetry(""),
		}}, nil
	}
	return nil, nil
}

// toolResultBlockType is a user line's content block that answers a tool call.
const toolResultBlockType = "tool_result"

// assistantEvents maps one assistant message's content blocks onto events:
// thinking, the model's prose, and tool invocations. A block with nothing in
// it is no event.
func (ev harnessEvent) assistantEvents() []Event {
	if ev.Message == nil {
		return nil
	}
	var out []Event
	for _, block := range ev.Message.Content {
		switch block.Type {
		case "thinking":
			if block.Thinking != "" {
				out = append(out, Event{Kind: EventThinking, Text: block.Thinking})
			}
		case "text":
			if block.Text != "" {
				out = append(out, Event{Kind: EventProgress, Text: block.Text})
			}
		case "tool_use":
			out = append(out, Event{Kind: EventActivity, Tool: block.Name, Input: block.Input})
		}
	}
	return out
}

// toolResultEvents is one EventToolResult per tool_result block on a user
// line: a line carries one block per tool call it answers, and parallel calls
// share a line. A block's content goes on the event as the answer's scrub
// reads it (toolResultRaw): a JSON string as it is, a list of content blocks
// as one JSON list of its text blocks' texts (an image's data is not read).
// A block with no content is still an event, with none.
func (ev harnessEvent) toolResultEvents() []Event {
	if ev.Message == nil {
		return nil
	}
	var out []Event
	for _, block := range ev.Message.Content {
		if block.Type != toolResultBlockType {
			continue
		}
		e := Event{Kind: EventToolResult}
		if len(block.Content) > 0 {
			e.ToolResult = toolResultRaw(block.Content)
		}
		out = append(out, e)
	}
	return out
}

// toolResultRaw is the raw JSON CollectToolResult reads for one tool_result
// block's content: a string as it is; a list of blocks as the list of its
// text blocks' texts; anything else as it is.
func toolResultRaw(content json.RawMessage) json.RawMessage {
	var blocks []struct {
		Type string `json:"type"`
		Text string `json:"text"`
	}
	if json.Unmarshal(content, &blocks) != nil {
		return content
	}
	texts := make([]string, 0, len(blocks))
	for _, b := range blocks {
		if b.Type == "text" && b.Text != "" {
			texts = append(texts, b.Text)
		}
	}
	raw, err := json.Marshal(texts)
	if err != nil {
		return content
	}
	return raw
}

// harnessEvent is one stream-json line from the harness stdout. Only the
// fields the mapper consults; unknown fields and unknown types pass through
// the decoder untouched and are ignored, mirroring the envelope's own
// unknown-field rule.
type harnessEvent struct {
	Type    string `json:"type"`
	Subtype string `json:"subtype"`
	// type:"assistant" carries an API Message; content blocks are inspected
	// for text / thinking / tool_use. type:"user" carries the tool_result
	// blocks the harness fed back to the model.
	Message *harnessMessage `json:"message,omitempty"`
	// type:"result" fields.
	Result  string `json:"result,omitempty"`
	IsError bool   `json:"is_error,omitempty"`
	// What the turn cost, on a result line: the harness's own token counts
	// for the turn, the turn's wall time, and the harness's running price
	// for the whole process. Usage and DurationMs are per turn;
	// TotalCostUSD is cumulative across every result line the process has
	// emitted, plus what a resumed session had already spent, so the
	// adapter publishes the difference between consecutive lines
	// (adapter.turnCost), never the figure itself. Each is optional, and
	// each is held raw and decoded on its own (telemetry, model): telemetry
	// in a shape the adapter does not expect is dropped field by field,
	// never the result line that carries the deliverable. The adapter
	// publishes what decodes as the usage artifact (payload spec "Reserved
	// artifact names") and nothing when nothing does.
	Usage        json.RawMessage `json:"usage,omitempty"`
	TotalCostUSD json.RawMessage `json:"total_cost_usd,omitempty"`
	DurationMs   json.RawMessage `json:"duration_ms,omitempty"`
	// type:"system" subtype:"init".
	SessionID string `json:"session_id,omitempty"`
	// Model is the model the harness runs, named once on the init line and
	// carried onto every turn's usage. Raw for the same reason.
	Model json.RawMessage `json:"model,omitempty"`
}

// harnessUsage is a result line's token counts. The three prompt figures
// are what the model read for the turn, split by cache state; the usage
// artifact sums them as inputTokens, since a cached read is still a read.
// float64 so a count written with a fraction still decodes; the part
// carries whole tokens.
type harnessUsage struct {
	InputTokens              float64 `json:"input_tokens,omitempty"`
	OutputTokens             float64 `json:"output_tokens,omitempty"`
	CacheCreationInputTokens float64 `json:"cache_creation_input_tokens,omitempty"`
	CacheReadInputTokens     float64 `json:"cache_read_input_tokens,omitempty"`
}

// model is the init line's model name, and whether the line carried one in
// a shape the adapter reads; an absent model is "" and not dropped.
func (ev harnessEvent) model() (name string, dropped bool) {
	if len(ev.Model) == 0 {
		return "", false
	}
	if json.Unmarshal(ev.Model, &name) != nil {
		return "", true
	}
	return name, false
}

// resultTelemetry is one result line's figures for the usage artifact, read
// field by field so one off-shape field costs that field alone: the
// per-turn usage (model from the init line, every prompt token as input, the
// wall time as the harness reported it; zero where the line had nothing),
// the running total and whether the line carried one, and the names of the
// fields that were present but did not decode, for the one warning the
// adapter logs per line. The cost is not in the usage: it is the running
// total's difference, which the adapter keeps.
type resultTelemetry struct {
	usage    lib.Usage
	total    float64
	hasTotal bool
	dropped  []string
}

func (ev harnessEvent) telemetry(model string) resultTelemetry {
	t := resultTelemetry{usage: lib.Usage{Model: model}}
	// Each figure passes lib.UsageFigure: a negative or overflowing number
	// is that field dropped and named, never a refused part or a wrapped
	// value, so the line's other figures still reach the bus.
	var ms float64
	var ok bool
	switch {
	case len(ev.DurationMs) == 0:
	case json.Unmarshal(ev.DurationMs, &ms) != nil:
		t.dropped = append(t.dropped, "duration_ms")
	default:
		if t.usage.DurationMs, ok = lib.UsageFigure(ms); !ok {
			t.dropped = append(t.dropped, "duration_ms")
		}
	}
	var counts harnessUsage
	switch {
	case len(ev.Usage) == 0:
	case json.Unmarshal(ev.Usage, &counts) != nil:
		t.dropped = append(t.dropped, "usage")
	default:
		if t.usage.InputTokens, ok = lib.UsageFigure(counts.InputTokens + counts.CacheCreationInputTokens + counts.CacheReadInputTokens); !ok {
			t.dropped = append(t.dropped, "usage.input_tokens")
		}
		if t.usage.OutputTokens, ok = lib.UsageFigure(counts.OutputTokens); !ok {
			t.dropped = append(t.dropped, "usage.output_tokens")
		}
	}
	// A null total is a line that carried none, not a total of zero: a zero
	// would read as the total going down and rebase the baseline.
	var total *float64
	switch {
	case len(ev.TotalCostUSD) == 0:
	case json.Unmarshal(ev.TotalCostUSD, &total) != nil:
		t.dropped = append(t.dropped, "total_cost_usd")
	case total != nil:
		t.total, t.hasTotal = *total, true
	}
	return t
}

type harnessMessage struct {
	Content harnessContent `json:"content"`
}

// harnessContent is a message's content blocks. A user message's content may
// be a plain string (a prompt the harness echoes) rather than a list; it
// carries no block the adapter reads, so it decodes as none rather than
// failing the whole line.
type harnessContent []harnessBlock

func (c *harnessContent) UnmarshalJSON(b []byte) error {
	var s string
	if json.Unmarshal(b, &s) == nil {
		*c = nil
		return nil
	}
	var blocks []harnessBlock
	if err := json.Unmarshal(b, &blocks); err != nil {
		return err
	}
	*c = blocks
	return nil
}

type harnessBlock struct {
	Type     string          `json:"type"`
	Text     string          `json:"text,omitempty"`
	Thinking string          `json:"thinking,omitempty"`
	Name     string          `json:"name,omitempty"`  // tool_use
	Input    json.RawMessage `json:"input,omitempty"` // tool_use
	// Content is a tool_result block's result, as the harness reported it
	// on a user message: a JSON string, or a list of content blocks (text,
	// image). Read only for the answer's scrub (toolResultEvents).
	Content json.RawMessage `json:"content,omitempty"`
}

// userMessage is the stream-json stdin shape for both the opening prompt and
// every steer: the SDK serializes user turns exactly like this, and a line
// written mid-run is absorbed at the harness's next turn boundary - which is
// the payload spec's steering rule made concrete.
type userMessage struct {
	Type    string          `json:"type"`
	Message userMessageBody `json:"message"`
}

type userMessageBody struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

// withResume is argv continuing harness session id, or argv unchanged when
// there is none. It copies rather than appending in place: argv is the pod's
// shared Config value, and every task builds its own from it.
func withResume(argv []string, id string) []string {
	if id == "" {
		return argv
	}
	return slices.Concat(argv, []string{harnessResumeFlag, id})
}
