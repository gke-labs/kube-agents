package main

import (
	"bufio"
	"bytes"
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib/persona"
	workeradapter "github.com/gke-labs/kube-agents/a2a/worker-adapter"
)

// TestAgysArgvIsItsHeadlessStreamJSONMode: agy's print switch takes a value
// (`-p=`), the message comes on stdin as stream-json, and --print-timeout
// sits a margin below the turn budget so agy ends the turn with a result
// before the adapter stops it. A2A_HARNESS_EXTRA_ARGS still appends.
func TestAgysArgvIsItsHeadlessStreamJSONMode(t *testing.T) {
	got := agyBuilder{}.command(harnessSpec{
		path:       "/opt/agy/bin/agy",
		model:      "ignored",
		maxTurns:   "40",
		turnBudget: 1790 * time.Second,
		extra:      []string{"--debug"},
	})
	want := []string{
		"/opt/agy/bin/agy", "-p=",
		"--input-format", "stream-json",
		"--output-format", "stream-json",
		"--print-timeout", "28m50s",
		"--debug",
	}
	if !slices.Equal(got, want) {
		t.Errorf("argv\n got %q\nwant %q", got, want)
	}
}

// TestAgysArgvCarriesNoneOfClaudeCodesPolicy: the tool lists, the delegate
// server and the prompts are Claude Code flags until #6711 and #6712 give
// agy its own; the builder leaves them out rather than passing flags agy
// would refuse or misread. The model and the turn count have no agy flag.
func TestAgysArgvCarriesNoneOfClaudeCodesPolicy(t *testing.T) {
	got := agyBuilder{}.command(harnessSpec{
		path:       "agy",
		model:      "claude-model",
		maxTurns:   "40",
		tools:      true,
		allowed:    "Read,Bash",
		disallowed: "Write",
		delegate:   &delegateServer{command: "/self", socket: "/run/s.sock"},
		prompts:    []string{"a prompt"},
		turnBudget: time.Hour,
	})
	for _, a := range got {
		for _, bad := range []string{"--allowedTools", "--disallowedTools", "--mcp-config", "--append-system-prompt",
			"--model", "--max-turns", "--setting-sources", "--strict-mcp-config", "--print", "--verbose",
			"Read,Bash", "claude-model", "a prompt"} {
			if a == bad {
				t.Errorf("agy's argv %q carries Claude Code's %q", got, bad)
			}
		}
	}
}

// TestAgysPrintTimeoutHasAFloor: a turn budget at or below the margin, as in
// a test with a short deadline, gets the floor; the adapter's own deadline
// ends such a turn first.
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
		if got := agyPrintTimeout(c.budget); got != c.want {
			t.Errorf("agyPrintTimeout(%v) = %v, want %v", c.budget, got, c.want)
		}
	}
}

// TestTheTurnBudgetFollowsTheDeadlineVariables: the budget is the task
// deadline less the kill grace, read from the variables Config reads.
func TestTheTurnBudgetFollowsTheDeadlineVariables(t *testing.T) {
	t.Setenv("A2A_TASK_DEADLINE_SECONDS", "")
	t.Setenv("A2A_KILL_GRACE_SECONDS", "")
	if got, want := harnessTurnBudget(), (defaultTaskDeadlineSeconds-defaultKillGraceSeconds)*time.Second; got != want {
		t.Errorf("default budget %v, want %v", got, want)
	}
	t.Setenv("A2A_TASK_DEADLINE_SECONDS", "600")
	t.Setenv("A2A_KILL_GRACE_SECONDS", "20")
	if got := harnessTurnBudget(); got != 580*time.Second {
		t.Errorf("budget %v, want 9m40s", got)
	}
}

// TestAgysEnvIsThePodsOwn: the builder adds nothing to the pod environment,
// and agy has no user settings file or default path here.
func TestAgysEnvIsThePodsOwn(t *testing.T) {
	base := []string{"HOME=/h", "AGY_LLM_GATEWAY_URL=http://127.0.0.1:1"}
	got := agyBuilder{}.env(slices.Clone(base), func(string) bool { return false })
	if !slices.Equal(got, base) {
		t.Errorf("env %q, want the pod's own %q", got, base)
	}
	if s := (agyBuilder{}).userSettings(func(string) string { return "/h" }); s != "" {
		t.Errorf("userSettings %q, want none", s)
	}
	if p := (agyBuilder{}).defaultPath(); p != "" {
		t.Errorf("defaultPath %q, want none (the image is #6715's)", p)
	}
}

// TestAnAgyTurnRunsOnTheBuiltArgv: the argv the builder writes, with the
// driver's resume flag, runs a stub agy that refuses any other argv; the
// driver's stdin line reaches it and the driver's decoder reads its output
// to a completed result. The argv, the stdin and the decoder agree.
func TestAnAgyTurnRunsOnTheBuiltArgv(t *testing.T) {
	fixture, err := filepath.Abs("../../worker-adapter/testdata/agy/basic.jsonl")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(fixture); err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	stub := filepath.Join(dir, "agy")
	script := `#!/bin/sh
want='-p= --input-format stream-json --output-format stream-json --print-timeout 28m50s --conversation conv-1'
[ "$*" = "$want" ] || { echo "argv: $*" >&2; exit 9; }
read -r first || exit 8
case "$first" in '{"event":"user"'*) ;; *) echo "stdin: $first" >&2; exit 7 ;; esac
cat "` + fixture + `"
`
	if err := os.WriteFile(stub, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	var driver workeradapter.Driver = mustDriver(t, persona.Antigravity)
	argv := driver.WithResume(agyBuilder{}.command(harnessSpec{path: stub, turnBudget: 1790 * time.Second}), "conv-1")
	in, err := driver.EncodeUser("say pong")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	cmd.Stdin = bytes.NewReader(append(in, '\n'))
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		t.Fatalf("stub agy: %v: %s", err, stderr.String())
	}
	dec := driver.NewDecoder()
	var result *workeradapter.Event
	sc := bufio.NewScanner(bytes.NewReader(out))
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	for sc.Scan() {
		evs, err := dec.Decode(sc.Bytes())
		if err != nil {
			t.Fatalf("decode %q: %v", sc.Text(), err)
		}
		for i := range evs {
			if evs[i].Kind == workeradapter.EventResult {
				result = &evs[i]
			}
		}
	}
	if result == nil {
		t.Fatalf("no result in %q", out)
	}
	if result.Failed || strings.TrimSpace(result.Text) != "pong" {
		t.Errorf("result failed=%v token=%q text=%q, want a completed pong", result.Failed, result.Token, result.Text)
	}
}

func mustDriver(t *testing.T, h persona.Harness) workeradapter.Driver {
	t.Helper()
	d, err := workeradapter.DriverFor(h)
	if err != nil {
		t.Fatal(err)
	}
	return d
}
