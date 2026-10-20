package workeradapter

import (
	"context"
	"encoding/json"
	"fmt"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// A result line whose total_cost_usd is null carried no total: the baseline
// is left alone rather than rebased to zero. A string total is dropped and
// named; an absent key is neither.
func TestNullCostTotalIsAbsentNotZero(t *testing.T) {
	for _, tc := range []struct {
		raw      string
		hasTotal bool
		dropped  bool
	}{{``, false, false}, {`null`, false, false}, {`"0.01"`, false, true}, {`0.01`, true, false}} {
		ev := harnessEvent{TotalCostUSD: json.RawMessage(tc.raw)}
		got := ev.telemetry("m")
		if got.hasTotal != tc.hasTotal || (len(got.dropped) > 0) != tc.dropped {
			t.Errorf("total_cost_usd %q: hasTotal=%v dropped=%v, want %v %v", tc.raw, got.hasTotal, got.dropped, tc.hasTotal, tc.dropped)
		}
	}
	a := &adapter{}
	a.turnCost(0.010)
	ev := harnessEvent{TotalCostUSD: json.RawMessage(`null`)}
	if tel := ev.telemetry("m"); tel.hasTotal {
		t.Fatal("null read as a total")
	}
	if got := a.turnCost(0.015); got-0.005 > 1e-9 || got-0.005 < -1e-9 {
		t.Fatalf("after a null line turnCost = %v, want 0.005 against the untouched baseline", got)
	}
}

// A negative or overflowing figure is that field dropped, never the part: a
// result line with a negative duration and a token count past int64 still
// publishes its other count and its cost, and the baseline advances. A
// negative total is unknown and leaves the baseline alone.
func TestOffShapeFiguresDropTheFieldNotThePart(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-otter-u6", "task-usage-6"
	submit(t, c, session, taskID, "odd figures")

	harness := stub(t, `
echo '{"type":"system","subtype":"init","session_id":"stub-u6","model":"m"}'
read first || exit 1
echo '{"type":"result","subtype":"success","result":"still here","duration_ms":-5,"total_cost_usd":0.02,"usage":{"input_tokens":1e19,"output_tokens":4}}'
`)
	out := waitOutcome(t, runAdapter(context.Background(), adapterConfig(url, taskID, session, harness)), 30*time.Second)
	if out.err != nil || out.res.State != lib.StateCompleted {
		t.Fatalf("run: state=%q err=%v", out.res.State, out.err)
	}
	got := usageParts(t, foldTask(t, c, session, taskID))
	if len(got) != 1 || got[0].DurationMs != 0 || got[0].InputTokens != 0 || got[0].OutputTokens != 4 || got[0].CostUsd != 0.02 {
		t.Fatalf("usage = %+v, want output tokens 4 and cost 0.02 with the duration and input count dropped", got)
	}

	a := &adapter{}
	a.turnCost(0.010)
	if cost, seen, known := a.turnCostFrom(-1); cost != 0 || seen != 0.010 || !known {
		t.Fatalf("negative total: cost=%v seen=%v known=%v, want unknown with the baseline untouched", cost, seen, known)
	}
}

// costStub is recordingStub's shape with a result line that carries the
// harness's running total by run number: 0.01, 0.03, 0.06, so the second and
// third runs' turns cost 0.02 and 0.03 when the baseline is carried across
// runs, and 0 (unknown) when it is not.
func costStub(t *testing.T, dir string) []string {
	t.Helper()
	return stub(t, fmt.Sprintf(`
dir=%q
n=$(( $(cat "$dir/n" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$dir/n"
echo '{"type":"system","subtype":"init","session_id":"sess-'"$n"'","model":"m"}'
read first || exit 1
case "$n" in 1) c=0.01;; 2) c=0.03;; *) c=0.06;; esac
echo '{"type":"result","subtype":"success","result":"answer '"$n"'","duration_ms":10,"total_cost_usd":'"$c"'}'
`, dir))
}

// Through the session loop: a reused pod hands the running total its last
// run read to the next run, so each later task's one result line prices its
// own turn. Remove the hand-off in session.go or execute and the second and
// third parts carry no cost.
func TestAReusedPodCarriesTheCostBaselineAcrossRuns(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session = "chat-otter-r1cost"
	dir := t.TempDir()
	submit(t, c, session, "task-rc-1", "first question")

	cfg := reuseConfig(url, "task-rc-1", session, costStub(t, dir))
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := runAdapter(ctx, cfg)

	waitState(t, c, session, "task-rc-1", lib.StateCompleted)
	submit(t, c, session, "task-rc-2", "second question")
	waitState(t, c, session, "task-rc-2", lib.StateCompleted)
	submit(t, c, session, "task-rc-3", "third question")
	waitState(t, c, session, "task-rc-3", lib.StateCompleted)

	for task, want := range map[string]float64{"task-rc-1": 0.01, "task-rc-2": 0.02, "task-rc-3": 0.03} {
		got := usageParts(t, foldTask(t, c, session, task))
		if len(got) != 1 || got[0].CostUsd-want > 1e-9 || got[0].CostUsd-want < -1e-9 {
			t.Errorf("%s usage = %+v, want one part costing %v", task, got, want)
		}
	}
	cancel()
	waitOutcome(t, done, 30*time.Second)
}

// usageParts is the usage artifact's parts read as Usage, in stream order.
func usageParts(t *testing.T, task *lib.Task) []lib.Usage {
	t.Helper()
	a := task.Artifact(lib.ArtifactUsage)
	if a == nil {
		return nil
	}
	var out []lib.Usage
	for _, p := range a.Parts {
		u, ok := lib.UsageOf(p)
		if !ok {
			t.Fatalf("usage part is not usage: %s", p.Data)
		}
		out = append(out, u)
	}
	return out
}

// The harness result line's usage, total_cost_usd and duration_ms reach the
// bus as one usage part on the turn, with the model the init line named;
// cached prompt tokens count as input, since the model read them.
func TestLifecycle_UsageArtifact(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-otter-u1", "task-usage-1"
	submit(t, c, session, taskID, "how many pods are pending")

	harness := stub(t, `
echo '{"type":"system","subtype":"init","session_id":"stub-u1","model":"claude-opus-5-5"}'
read first || exit 1
echo '{"type":"assistant","message":{"content":[{"type":"text","text":"three"}]}}'
echo '{"type":"result","subtype":"success","result":"three pods are pending","duration_ms":4321,"duration_api_ms":4000,"num_turns":1,"total_cost_usd":0.0456,"usage":{"input_tokens":100,"cache_creation_input_tokens":20,"cache_read_input_tokens":30,"output_tokens":7}}'
`)
	out := waitOutcome(t, runAdapter(context.Background(), adapterConfig(url, taskID, session, harness)), 30*time.Second)
	if out.err != nil || out.res.State != lib.StateCompleted {
		t.Fatalf("run: state=%q err=%v", out.res.State, out.err)
	}
	task := foldTask(t, c, session, taskID)
	if err := task.ValidateArtifacts(); err != nil {
		t.Fatalf("artifacts: %v", err)
	}
	got := usageParts(t, task)
	want := []lib.Usage{{Model: "claude-opus-5-5", InputTokens: 150, OutputTokens: 7, CostUsd: 0.0456, DurationMs: 4321}}
	if len(got) != 1 || got[0] != want[0] {
		t.Fatalf("usage = %+v, want %+v", got, want)
	}
	// The usage lands before the terminal: nothing follows the final event
	// (assertion 10), so a usage part in the fold is one that preceded it.
	events := replayEvents(t, url, session, taskID)
	last := events[len(events)-1]
	if last.Kind != lib.KindStatusUpdate {
		t.Fatalf("last event is %s, not the terminal", last.Kind)
	}
}

// A result line with no usage figures publishes no usage artifact: the
// reader sees absence, never zeroes.
func TestUsageArtifactAbsentWithoutFigures(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-otter-u2", "task-usage-2"
	submit(t, c, session, taskID, "say hi")

	harness := stub(t, `
echo '{"type":"system","subtype":"init","session_id":"stub-u2"}'
read first || exit 1
echo '{"type":"result","subtype":"success","result":"hi"}'
`)
	out := waitOutcome(t, runAdapter(context.Background(), adapterConfig(url, taskID, session, harness)), 30*time.Second)
	if out.err != nil || out.res.State != lib.StateCompleted {
		t.Fatalf("run: state=%q err=%v", out.res.State, out.err)
	}
	task := foldTask(t, c, session, taskID)
	if task.Artifact(lib.ArtifactUsage) != nil {
		t.Fatalf("usage artifact published with no figures: %+v", task.Artifact(lib.ArtifactUsage))
	}
}

// A turn that errored still cost tokens: its usage is on the stream ahead of
// the failed terminal.
func TestUsageArtifactOnHarnessError(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-otter-u3", "task-usage-3"
	submit(t, c, session, taskID, "do the thing")

	harness := stub(t, `
echo '{"type":"system","subtype":"init","session_id":"stub-u3","model":"claude-opus-5-5"}'
read first || exit 1
echo '{"type":"result","subtype":"error_max_turns","is_error":true,"result":"ran out of turns","duration_ms":99,"usage":{"input_tokens":5,"output_tokens":1}}'
`)
	out := waitOutcome(t, runAdapter(context.Background(), adapterConfig(url, taskID, session, harness)), 30*time.Second)
	if out.res.State != lib.StateFailed {
		t.Fatalf("run: state=%q err=%v", out.res.State, out.err)
	}
	task := foldTask(t, c, session, taskID)
	if err := task.ValidateArtifacts(); err != nil {
		t.Fatalf("artifacts: %v", err)
	}
	got := usageParts(t, task)
	if len(got) != 1 || got[0].InputTokens != 5 || got[0].OutputTokens != 1 || got[0].DurationMs != 99 || got[0].Model != "claude-opus-5-5" {
		t.Fatalf("usage = %+v", got)
	}
}

// The harness's total_cost_usd is a running total for the process, so a
// turn's cost is the difference from the line before; the first line of a
// fresh process is the total, the first line of a resumed session is unknown,
// and a total that went down is unknown.
func TestTurnCostIsTheRunningTotalsDifference(t *testing.T) {
	near := func(got, want float64) bool { return got-want < 1e-9 && want-got < 1e-9 }
	fresh := &adapter{}
	for i, tc := range []struct{ total, want float64 }{{0.010, 0.010}, {0.015, 0.005}, {0.015, 0}, {0.040, 0.025}, {0.030, 0}} {
		if got := fresh.turnCost(tc.total); !near(got, tc.want) {
			t.Fatalf("line %d: turnCost(%v) = %v, want %v", i, tc.total, got, tc.want)
		}
	}
	resumed := &adapter{resumedSession: true}
	if got := resumed.turnCost(0.5); got != 0 {
		t.Fatalf("resumed first line: turnCost = %v, want unknown", got)
	}
	if got := resumed.turnCost(0.52); !near(got, 0.02) {
		t.Fatalf("resumed second line: turnCost = %v, want 0.02", got)
	}
	// The next run in the same pod resumes the session with the baseline the
	// run before left (session.go hands it on through turnPlan), so its
	// first line prices its own turn rather than reading as unknown.
	carried := &adapter{resumedSession: true, costSeen: resumed.costSeen, costKnown: resumed.costKnown}
	if got := carried.turnCost(0.55); !near(got, 0.03) {
		t.Fatalf("carried baseline: turnCost = %v, want 0.03", got)
	}
}

// Telemetry in a shape the adapter does not expect is dropped, never the
// result line: a fractional duration, a token count written as a string and
// a model that is not a string still leave the deliverable and the terminal
// intact, and the usage part carries what did decode.
func TestOffShapeTelemetryDoesNotLoseTheResult(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-otter-u5", "task-usage-5"
	submit(t, c, session, taskID, "odd shapes")

	harness := stub(t, `
echo '{"type":"system","subtype":"init","session_id":"stub-u5","model":7}'
read first || exit 1
echo '{"type":"result","subtype":"success","result":"still here","duration_ms":12.5,"total_cost_usd":"0.01","usage":{"input_tokens":"10","output_tokens":2}}'
`)
	out := waitOutcome(t, runAdapter(context.Background(), adapterConfig(url, taskID, session, harness)), 30*time.Second)
	if out.err != nil || out.res.State != lib.StateCompleted {
		t.Fatalf("run: state=%q err=%v", out.res.State, out.err)
	}
	task := foldTask(t, c, session, taskID)
	if got := artifactText(task, lib.ArtifactResult); got != "still here" {
		t.Fatalf("result artifact %q", got)
	}
	got := usageParts(t, task)
	if len(got) != 1 || got[0].DurationMs != 12 || got[0].Model != "" || got[0].CostUsd != 0 || got[0].InputTokens != 0 {
		t.Fatalf("usage = %+v, want the duration alone", got)
	}
}

// Two result lines from one process carry the running total; the usage
// parts carry each turn's share, so a reader that sums them gets the total
// once, not twice.
func TestUsageCostIsPerTurnAcrossResultLines(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-otter-u4", "task-usage-4"
	submit(t, c, session, taskID, "two results")

	harness := stub(t, `
echo '{"type":"system","subtype":"init","session_id":"stub-u4","model":"m"}'
read first || exit 1
echo '{"type":"result","subtype":"success","result":"first","duration_ms":10,"total_cost_usd":0.010,"usage":{"input_tokens":10,"output_tokens":1}}'
echo '{"type":"result","subtype":"success","result":"second","duration_ms":20,"total_cost_usd":0.015,"usage":{"input_tokens":20,"output_tokens":2}}'
`)
	out := waitOutcome(t, runAdapter(context.Background(), adapterConfig(url, taskID, session, harness)), 30*time.Second)
	if out.err != nil || out.res.State != lib.StateCompleted {
		t.Fatalf("run: state=%q err=%v", out.res.State, out.err)
	}
	got := usageParts(t, foldTask(t, c, session, taskID))
	if len(got) != 2 || got[0].CostUsd != 0.010 || got[1].CostUsd-0.005 > 1e-9 || got[1].CostUsd-0.005 < -1e-9 {
		t.Fatalf("usage = %+v, want costs 0.010 and 0.005", got)
	}
	if total := lib.UsageTotal(got); total.CostUsd-0.015 > 1e-9 || total.CostUsd-0.015 < -1e-9 || total.InputTokens != 30 {
		t.Fatalf("total = %+v, want cost 0.015 and 30 input tokens", total)
	}
}
