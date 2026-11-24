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
