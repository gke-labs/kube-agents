package workeradapter

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/persona"
	"github.com/gke-labs/kube-agents/a2a/worker-adapter/isolation"
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
//     adapter publishes none, until #6808 takes the per-turn cost from
//     LiteLLM through the loopback proxy (#6714);
//   - result.usage and duration_seconds are cumulative over the
//     conversation, across a --conversation resume too, so the decoder
//     reports each result's difference from the conversation's last
//     (agyUsageLedger), whichever process reported it.
//
// Before every agy process starts, the first included, PrepareTurn clears
// agy's configuration entries from the workspace and its conversation
// directories (the isolation package, #6710), and agy takes one user turn
// per process (RestartsPerTurn), so nothing an earlier turn started is
// still running when that happens. Its MCP configuration (#6712),
// permissions (#6711) and model routing (#6714) are other sub-issues of
// #6707; until they land the driver configures no MCP server.
type agyDriver struct {
	// iso picks the isolation table from the agy binary's version. Nil
	// means the binary is unknown, and every turn is refused.
	iso *agyIsolation
	// ledger carries each conversation's running usage from one agy
	// process to the next. Nil counts from zero in every process.
	ledger *agyUsageLedger
}

// agyUsageLedger is each conversation's last accepted cumulative usage and
// duration, by conversation id. One agy process takes one user turn and a
// later one resumes the conversation; agy's figures keep running across
// the resume, so a new process's decoder starts from where the last left
// off rather than from zero.
type agyUsageLedger struct {
	mu sync.Mutex
	by map[string]agyCumulative
}

type agyCumulative struct{ input, output, duration float64 }

func (l *agyUsageLedger) get(conv string) (agyCumulative, bool) {
	if l == nil || conv == "" {
		return agyCumulative{}, false
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	c, ok := l.by[conv]
	return c, ok
}

func (l *agyUsageLedger) put(conv string, c agyCumulative) {
	if l == nil || conv == "" {
		return
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.by == nil {
		l.by = map[string]agyCumulative{}
	}
	l.by[conv] = c
}

var _ Driver = agyDriver{}

// NewAgyDriver is the driver for the agy binary at bin, whose version
// chooses the isolation table on the first PrepareTurn.
func NewAgyDriver(bin string) agyDriver {
	return agyDriver{iso: &agyIsolation{bin: bin}, ledger: &agyUsageLedger{}}
}

// agyIsolation reads the agy binary's version and keeps the table for
// every later turn. A failed read is not kept: a timeout or a canceled
// context is tried again at the next turn, which is refused meanwhile.
type agyIsolation struct {
	bin   string
	mu    sync.Mutex
	known bool
	table isolation.Table
}

// agyOthersSettle bounds how long PrepareTurn waits for processes the
// last kill reached to be gone; agyOthersPoll is the interval.
var (
	agyOthersSettle = 2 * time.Second
	agyOthersPoll   = 20 * time.Millisecond
)

// agyVersionTimeout bounds `agy --version`.
const agyVersionTimeout = 30 * time.Second

func (i *agyIsolation) resolve(ctx context.Context) (isolation.Table, error) {
	i.mu.Lock()
	defer i.mu.Unlock()
	if i.known {
		return i.table, nil
	}
	t, err := readAgyTable(ctx, i.bin)
	if err != nil {
		return isolation.Table{}, err
	}
	i.table, i.known = t, true
	return t, nil
}

// readAgyTable runs `agy --version` from an empty directory with an empty
// HOME and a bare environment, so reading the version loads no workspace
// or HOME configuration, and picks the table for exactly that version.
func readAgyTable(ctx context.Context, bin string) (isolation.Table, error) {
	if bin == "" {
		return isolation.Table{}, errors.New("no agy binary named")
	}
	dir, err := os.MkdirTemp("", "agy-version-")
	if err != nil {
		return isolation.Table{}, err
	}
	defer os.RemoveAll(dir)
	ctx, cancel := context.WithTimeout(ctx, agyVersionTimeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, bin, "--version") // #nosec G204 -- the configured harness binary, no shell
	cmd.Dir = dir
	cmd.Env = []string{"HOME=" + dir, "PATH=/usr/local/bin:/usr/bin:/bin"}
	// Its own process group, killed whole once it returns, so nothing it
	// started outlives the read and trips the no-other-process check.
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	out, err := cmd.Output()
	if cmd.Process != nil {
		_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
	}
	if err != nil {
		return isolation.Table{}, fmt.Errorf("agy --version: %w", err)
	}
	return isolation.ForVersion(string(out))
}

// otherProcesses lists the live processes in the pid namespace besides the
// adapter (leftovers_linux.go); a variable so a test can scope it.
var otherProcesses = listOtherProcesses

// agyResumeFlag continues an agy conversation by id.
const agyResumeFlag = "--conversation"

// The reason tokens of an agy result the turn failed on. A status other
// than SUCCESS reuses the generic token whose meaning matches; the other
// two are agy's own, and the gateway words them (relay.go reasonSentences).
const (
	agyErrorToken            = "error_during_execution" // #nosec G101 -- a failure reason token, not a credential
	agyPrintTimeoutToken     = "print-timeout"
	agyPermissionDeniedToken = "permission-denied"
)

// agyDetailCap bounds the detail an agy failure carries into its reason:
// agy's error text is the upstream API's, and an HTML error page or a long
// list of skipped calls has no business in a one-line reason.
const agyDetailCap = 200

// msPerSecond turns agy's duration_seconds into the usage artifact's
// milliseconds.
const msPerSecond = float64(time.Second / time.Millisecond)

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

func (d agyDriver) NewDecoder() Decoder { return &agyDecoder{ledger: d.ledger} }

// WithResume resumes conversation id, replacing a --conversation already in
// argv: a later process of the task resumes the conversation agy reported,
// whatever the first one was told.
func (agyDriver) WithResume(argv []string, id string) []string {
	if id == "" {
		return argv
	}
	return append(withoutFlag(argv, agyResumeFlag), agyResumeFlag, id)
}

// withoutFlag is argv without flag and its value, in either the
// `flag value` or the `flag=value` form.
func withoutFlag(argv []string, flag string) []string {
	out := make([]string, 0, len(argv))
	for i := 0; i < len(argv); i++ {
		switch {
		case argv[i] == flag:
			i++ // and its value
		case strings.HasPrefix(argv[i], flag+"="):
		default:
			out = append(out, argv[i])
		}
	}
	return out
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

// PrepareTurn runs before every agy process starts. It refuses unless the
// adapter is the container's init and no other process is alive: as pid 1
// the adapter's killLeftovers ends every process in the namespace after a
// harness exits, setsid'd ones included, so anything still alive here is
// outside what that kill reaches (another user's, or a namespace shared
// with other containers) and could still write. Then it clears agy's
// configuration entries from the workspace and every conversation
// directory under HOME, logging each one it removes.
func (d agyDriver) PrepareTurn(ctx context.Context, dirs TurnDirs) error {
	if d.iso == nil {
		return errors.New("the agy binary is not known, so no isolation table applies")
	}
	table, err := d.iso.resolve(ctx)
	if err != nil {
		return err
	}
	if pid := adapterPID(); pid != containerInitPID {
		return fmt.Errorf("agy needs the adapter as the container's init (pid %d, not %d) to end each turn's processes", containerInitPID, pid)
	}
	// Briefly: a process the kill reached may not have finished dying.
	var others []int
	for deadline := time.Now().Add(agyOthersSettle); ; {
		var err error
		if others, err = otherProcesses(); err != nil {
			return fmt.Errorf("checking for processes left from an earlier turn: %w", err)
		}
		if len(others) == 0 || time.Now().After(deadline) {
			break
		}
		time.Sleep(agyOthersPoll)
	}
	if len(others) > 0 {
		return fmt.Errorf("%d processes outlived the last turn (pids %v)", len(others), others)
	}
	removed, err := table.PrepareTurn(dirs.Workspace, dirs.Home)
	for _, p := range removed {
		slog.Warn("removed a harness configuration entry before the turn", "path", p)
	}
	return err
}

const (
	// agyPrintTimeoutFlag bounds agy's wall clock for one user message. Its
	// value is a Go duration. On expiry agy ends the turn with a result whose
	// finished_reason is print_timeout, which the driver fails with the
	// print-timeout reason.
	agyPrintTimeoutFlag = "--print-timeout"
	// agyPrintTimeoutMargin is how far below the turn budget agy's own bound
	// sits, so agy writes its result before the adapter's deadline. Against
	// a gateway that answers 503 every time, agy 1.5.13 retried for 147s
	// (nine requests) before its ERROR result; a margin under that turns an
	// upstream outage into a print-timeout. 180s leaves room above it.
	agyPrintTimeoutMargin = 180 * time.Second
	// agyMinPrintTimeout is the floor: a turn budget at or below the margin
	// plus the floor (240s), as in tests with a short deadline, gets 60s,
	// and there the adapter's own deadline may end the turn first.
	agyMinPrintTimeout = 60 * time.Second
)

// AgyPrintTimeout is a turn budget less agyPrintTimeoutMargin, and never
// below agyMinPrintTimeout.
func AgyPrintTimeout(budget time.Duration) time.Duration {
	if d := budget - agyPrintTimeoutMargin; d > agyMinPrintTimeout {
		return d
	}
	return agyMinPrintTimeout
}

// WithTurnBudget sets agy's --print-timeout from left, the time this
// process has before the adapter stops it: the task's remaining deadline
// less the kill grace. Every agy process gets its own (one user turn per
// process, RestartsPerTurn), so a later turn's bound is what is left, not
// the whole task's. Any earlier --print-timeout in argv is replaced.
func (agyDriver) WithTurnBudget(argv []string, left time.Duration) []string {
	return append(withoutFlag(argv, agyPrintTimeoutFlag), agyPrintTimeoutFlag, AgyPrintTimeout(left).String())
}

// RestartsPerTurn is true: each user turn gets a fresh agy process, so a
// process an earlier turn left running cannot write between PrepareTurn
// and the next turn's start.
func (agyDriver) RestartsPerTurn() bool { return true }

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
	StepIndex *int         `json:"step_index"`
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
	// ledger and conv carry the conversation's running figures across
	// processes (agyUsageLedger); conv is set at init.
	ledger   *agyUsageLedger
	conv     string
	model    string
	input    float64
	output   float64
	duration float64
	// toolDone is the tool steps, by step_index, whose result is already
	// in: a step's terminal state is reported once however often agy
	// repeats it.
	toolDone map[int]bool
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
		d.conv = ln.ConversationID
		if c, ok := d.ledger.get(d.conv); ok {
			d.input, d.output, d.duration = c.input, c.output, c.duration
		}
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

// step maps a step_update onto progress, activity and tool results. The
// model's prose streams as agent_response text deltas; a tool step is
// announced once, when it starts, and its result is in when it first reaches
// DONE or ERROR (EventToolResult, once per step_index: the write-refusal
// check runs on it). User input, system messages and error messages carry
// nothing the artifacts show; the result decides failure.
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
	case s.StepType == "tool" && (s.State == "DONE" || s.State == "ERROR"):
		if s.StepIndex != nil {
			if d.toolDone[*s.StepIndex] {
				return nil
			}
			if d.toolDone == nil {
				d.toolDone = map[int]bool{}
			}
			d.toolDone[*s.StepIndex] = true
		}
		return []Event{{Kind: EventToolResult}}
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
// usage and duration and the last accepted one, which may come from an
// earlier process of the same conversation (agyUsageLedger). agy reports
// no cost (#6808), so there is no running total. duration_seconds runs
// across the conversation like the tokens (a resumed process's first
// result read 3.3s after the last process's 2.6s), so the difference
// includes any wait between one result and the next turn's start.
//
// A field that is absent or null is no figure at all, and leaves its
// baseline alone. A baseline moves only when its figure is accepted: a
// figure that comes out negative is dropped, and the next result still
// counts from the last good total rather than from the bad one.
func (d *agyDecoder) telemetry(r agyResult) resultTelemetry {
	t := resultTelemetry{usage: lib.Usage{Model: d.model}}
	var secs float64
	switch {
	case agyAbsent(r.DurationSeconds):
	case json.Unmarshal(r.DurationSeconds, &secs) != nil:
		t.dropped = append(t.dropped, "duration_seconds")
	default:
		if ms, ok := lib.UsageFigure((secs - d.duration) * msPerSecond); ok {
			t.usage.DurationMs = ms
			d.duration = secs
		} else {
			t.dropped = append(t.dropped, "duration_seconds")
		}
	}
	var u agyUsage
	switch {
	case agyAbsent(r.Usage):
	case json.Unmarshal(r.Usage, &u) != nil:
		t.dropped = append(t.dropped, "usage")
	default:
		input := u.InputTokens + u.CacheReadTokens
		if n, ok := lib.UsageFigure(input - d.input); ok {
			t.usage.InputTokens = n
			d.input = input
		} else {
			t.dropped = append(t.dropped, "usage.input_tokens")
		}
		if n, ok := lib.UsageFigure(u.OutputTokens - d.output); ok {
			t.usage.OutputTokens = n
			d.output = u.OutputTokens
		} else {
			t.dropped = append(t.dropped, "usage.output_tokens")
		}
	}
	d.ledger.put(d.conv, agyCumulative{input: d.input, output: d.output, duration: d.duration})
	return t
}

// agyAbsent reports a result field that carries no figure: missing, or a
// JSON null, which a json.RawMessage keeps as the four bytes "null".
func agyAbsent(raw json.RawMessage) bool {
	v := strings.TrimSpace(string(raw))
	return v == "" || v == "null"
}

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
	if len(names) == 0 {
		return fmt.Sprintf("skipped for want of approval: %d unnamed action(s)", len(actions))
	}
	return "skipped for want of approval: " + strings.Join(names, ", ")
}
