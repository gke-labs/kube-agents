package workeradapter

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// TestAgysPrintTimeoutHasAFloor: a turn budget at or below the margin plus
// the floor (120s), as in a test with a short deadline, gets the floor; the
// adapter's own deadline may end such a turn first.
func TestAgysPrintTimeoutHasAFloor(t *testing.T) {
	for _, c := range []struct {
		budget, want time.Duration
	}{
		{1790 * time.Second, 1730 * time.Second},
		{121 * time.Second, 61 * time.Second},
		{120 * time.Second, time.Minute},
		{90 * time.Second, time.Minute},
		{30 * time.Second, time.Minute},
		{0, time.Minute},
		{-time.Second, time.Minute},
	} {
		if got := AgyPrintTimeout(c.budget); got != c.want {
			t.Errorf("AgyPrintTimeout(%v) = %v, want %v", c.budget, got, c.want)
		}
	}
}

// TestAgysTurnBudgetReplacesAnEarlierOne: WithTurnBudget sets exactly one
// --print-timeout, replacing one already in argv in either form, and keeps
// every other argument in order.
func TestAgysTurnBudgetReplacesAnEarlierOne(t *testing.T) {
	d := agyDriver{}
	for _, in := range [][]string{
		{"agy", "-p=", "--debug"},
		{"agy", "-p=", "--print-timeout", "1h", "--debug"},
		{"agy", "--print-timeout=5m", "-p=", "--debug"},
		{"agy", "--print-timeout", "1h", "-p=", "--print-timeout=2h", "--debug"},
	} {
		got := d.WithTurnBudget(slices.Clone(in), 1790*time.Second)
		want := []string{"agy", "-p=", "--debug", "--print-timeout", "28m50s"}
		if !slices.Equal(got, want) {
			t.Errorf("WithTurnBudget(%q)\n got %q\nwant %q", in, got, want)
		}
	}
}

func printTimeoutOf(t *testing.T, argv string) time.Duration {
	t.Helper()
	f := strings.Fields(argv)
	i := slices.Index(f, agyPrintTimeoutFlag)
	if i < 0 || i+1 >= len(f) {
		t.Fatalf("argv %q has no %s", argv, agyPrintTimeoutFlag)
	}
	d, err := time.ParseDuration(f[i+1])
	if err != nil {
		t.Fatalf("argv %q: %v", argv, err)
	}
	return d
}

// TestEachAgyProcessGetsTheTimeLeft: the first process's --print-timeout is
// the task deadline less the kill grace, less the margin; the process a held
// steer starts gets what is left after the first turn, so less.
func TestEachAgyProcessGetsTheTimeLeft(t *testing.T) {
	dir := t.TempDir()
	harness := stub(t, fmt.Sprintf(`
dir=%q
n=$(( $(cat "$dir/runs" 2>/dev/null || echo 0) + 1 )); echo $n > "$dir/runs"
echo "$*" > "$dir/argv.$n"
read first || exit 1
echo '{"event":"init","conversation_id":"conv-b","init":{"model":"m"}}'
[ "$n" = 1 ] && { echo 1 > "$dir/turn1"; sleep 1.5; }
echo '{"event":"result","result":{"conversation_id":"conv-b","status":"SUCCESS","response":"turn '$n'"}}'
exit 0
`, dir))
	const session, taskID = "chat-agy-budget", "task-agy-budget"
	url := startServer(t)
	c := testClient(t, url)
	origin := submit(t, c, session, taskID, "start")
	cfg := adapterConfig(url, taskID, session, harness)
	useAgyTestDriver(t, &cfg)
	cfg.TaskDeadline = 10 * time.Minute
	done := runAdapter(context.Background(), cfg)
	waitFile(t, filepath.Join(dir, "turn1"), 10*time.Second)
	publishSteerFor(t, c, origin, session, "a steer during turn one")
	out := waitOutcome(t, done, 30*time.Second)
	if out.err != nil || out.res.State != lib.StateCompleted {
		t.Fatalf("state=%q err=%v", out.res.State, out.err)
	}

	first := printTimeoutOf(t, readRunIfAny(dir, "argv", 1))
	if want := AgyPrintTimeout(cfg.TaskDeadline - cfg.KillGrace); first != want {
		t.Errorf("first process --print-timeout %v, want %v (deadline less kill grace, less the margin)", first, want)
	}
	second := printTimeoutOf(t, readRunIfAny(dir, "argv", 2))
	if second >= first || second < first-time.Minute {
		t.Errorf("second process --print-timeout %v; want a little under the first's %v, since turn one used time", second, first)
	}
	if n := strings.Count(readRunIfAny(dir, "argv", 2), agyPrintTimeoutFlag); n != 1 {
		t.Errorf("second process argv carries %s %d times", agyPrintTimeoutFlag, n)
	}
	_ = os.Remove(filepath.Join(dir, "turn1"))
}

func agyResultUsage(t *testing.T, d Decoder, conv string, input, output, secs float64) lib.Usage {
	t.Helper()
	if _, err := d.Decode([]byte(`{"event":"init","conversation_id":"` + conv + `","init":{"model":"m"}}`)); err != nil {
		t.Fatal(err)
	}
	evs, err := d.Decode([]byte(fmt.Sprintf(`{"event":"result","result":{"conversation_id":%q,"status":"SUCCESS","response":"x","duration_seconds":%v,"usage":{"input_tokens":%v,"output_tokens":%v}}}`, conv, secs, input, output)))
	if err != nil || len(evs) != 1 || evs[0].Kind != EventResult {
		t.Fatalf("decode result: %v %v", evs, err)
	}
	return evs[0].Telemetry.usage
}

// TestAgyUsageCarriesAcrossProcessesOfAConversation: agy's usage and
// duration keep running across a --conversation resume, so a later
// process's first result counts from where the conversation's last one
// left off, not from zero. Without a ledger every process counts from zero.
func TestAgyUsageCarriesAcrossProcessesOfAConversation(t *testing.T) {
	d := agyDriver{ledger: &agyUsageLedger{}}
	first := agyResultUsage(t, d.NewDecoder(), "conv-u", 10, 5, 1.0)
	second := agyResultUsage(t, d.NewDecoder(), "conv-u", 20, 12, 2.5)
	if first.InputTokens != 10 || first.OutputTokens != 5 {
		t.Errorf("first process: %+v", first)
	}
	if second.InputTokens != 10 || second.OutputTokens != 7 || second.DurationMs != 1500 {
		t.Errorf("second process of the conversation: %+v; want 10 in, 7 out, 1500ms", second)
	}
	other := agyResultUsage(t, d.NewDecoder(), "conv-other", 4, 2, 0.5)
	if other.InputTokens != 4 {
		t.Errorf("another conversation borrowed a baseline: %+v", other)
	}
	plain := agyDriver{}
	_ = agyResultUsage(t, plain.NewDecoder(), "conv-u", 10, 5, 1.0)
	if again := agyResultUsage(t, plain.NewDecoder(), "conv-u", 20, 12, 2.5); again.InputTokens != 20 {
		t.Errorf("without a ledger a process counts from zero: %+v", again)
	}
}

// TestAgyVersionReadIsRetriedAfterAFailure: a failed `agy --version` is not
// kept; the next turn reads it again, and a good read is kept.
func TestAgyVersionReadIsRetriedAfterAFailure(t *testing.T) {
	dir := t.TempDir()
	count := filepath.Join(dir, "count")
	bin := filepath.Join(dir, "agy")
	script := fmt.Sprintf("#!/bin/sh\nn=$(( $(cat %q 2>/dev/null || echo 0) + 1 )); echo $n > %q\n[ $n = 1 ] && exit 1\necho 1.5.13\n", count, count)
	if err := os.WriteFile(bin, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	iso := &agyIsolation{bin: bin}
	if _, err := iso.resolve(context.Background()); err == nil {
		t.Fatal("the first, failing read succeeded")
	}
	for range 2 {
		if tb, err := iso.resolve(context.Background()); err != nil || tb.Version != "1.5.13" {
			t.Fatalf("read after a failure: %q %v", tb.Version, err)
		}
	}
	if b, _ := os.ReadFile(count); strings.TrimSpace(string(b)) != "2" {
		t.Errorf("agy --version ran %s times; want 2 (the failure, then one good read kept)", strings.TrimSpace(string(b)))
	}
}

// TestAgysResumeReplacesAnEarlierOne: WithResume leaves one --conversation,
// the one given, in either form of an earlier one.
func TestAgysResumeReplacesAnEarlierOne(t *testing.T) {
	d := agyDriver{}
	for _, in := range [][]string{
		{"agy", "-p="},
		{"agy", "--conversation", "old", "-p="},
		{"agy", "--conversation=old", "-p="},
	} {
		got := d.WithResume(slices.Clone(in), "new")
		if want := []string{"agy", "-p=", "--conversation", "new"}; !slices.Equal(got, want) {
			t.Errorf("WithResume(%q) = %q, want %q", in, got, want)
		}
	}
	if got := d.WithResume([]string{"agy"}, ""); !slices.Equal(got, []string{"agy"}) {
		t.Errorf("an empty id changed argv: %q", got)
	}
}
